"""Parent-tooling leak regression (coldstart_1108, run 20261108-fc2aaf6c).

The codex leg inherited the eval host's PATH, ran `command -v hermes`, found
the host's hermes wrapper — hardwired to the REAL ~/.hermes regardless of
$HOME — and answered install-unnamed-need by installing official/email/
agentmail from the Hermes Skills Hub (wrong marketplace) while writing into
/home/adam/.hermes/skills: a host mutation from inside the "isolated" cold
sandbox. HOME was isolated; the parent's TOOLING was not.

Four contracts pinned here:
1. Hermes legs keep the parent PATH and HERMES-shaped env verbatim (the
   hermes binary IS that harness; its writes go to the sandbox HERMES_HOME).
2. Non-hermes cold envs drop every HERMES-shaped variable and every PATH
   entry inside the real home — the eval host's agent CLIs must be
   unreachable from a cold codex/claude shell.
3. The one sanctioned real-home entry is the node runtime's directory
   (npm-shim harness CLIs bootstrap through `#!/usr/bin/env node`; that dir
   exports only node/npm/npx).
4. Codex/claude argv[0] is absolutized against the RUNNER's PATH —
   subprocess resolves bare names against the CHILD env's PATH, which the
   scrub no longer satisfies.

No real binary runs: subprocess.run is captured, shutil.which is faked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "evals" / "agent_coldstart"))

import run as run_mod  # noqa: E402

HOME = Path.home()


def _fake_which(node_dir: str, codex: str = "/opt/vendor/bin/codex", claude: str = "/opt/vendor/bin/claude"):
    """shutil.which stand-in: harness CLIs resolve OUTSIDE the home tree,
    node resolves INSIDE it (mirrors the eval box: ~/.local/bin/codex is a
    symlink, node lives under the real home)."""
    table = {
        "node": str(Path(node_dir) / "node"),
        "codex": codex,
        "claude": claude,
    }

    def _which(name, path=None):  # noqa: ARG001
        return table.get(name)

    return _which


def test_hermes_cold_env_keeps_parent_tooling(monkeypatch):
    """Contract 1: the hermes exemption is intentional and pinned."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HERMES_HOME", str(HOME / ".hermes"))
    env = run_mod.build_cold_env("hermes")
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HERMES_HOME"] == str(HOME / ".hermes")
    # the default argument is the hermes case (success_check, back-compat)
    assert run_mod.build_cold_env()["HERMES_HOME"] == str(HOME / ".hermes")


def test_codex_cold_env_scrubs_hermes_vars_and_home_paths(monkeypatch):
    """Contracts 2+3: no agent CLI from the real home, node runtime kept."""
    node_dir = str(HOME / ".hermes" / "node" / "bin")
    monkeypatch.setattr(run_mod.shutil, "which", _fake_which(node_dir))
    crafted = os.pathsep.join(
        [
            str(HOME / ".local" / "bin"),
            "/usr/local/bin",
            str(HOME / ".hermes" / "hermes-agent" / ".hermes" / "bin"),
            "/usr/bin",
            node_dir,
            "/usr/bin",  # dup: must survive exactly once
        ]
    )
    monkeypatch.setenv("PATH", crafted)
    monkeypatch.setenv("HERMES_HOME", str(HOME / ".hermes"))
    monkeypatch.setenv("HERMES_AGENT_ID", "host-agent")
    monkeypatch.setenv("LOOPSKILL_API_KEY", "ls_live_hostkey_000000000001")
    monkeypatch.setenv("GLM_API_KEY", "glm-keepme")

    env = run_mod.build_cold_env("codex")
    assert "HERMES_HOME" not in env
    assert "HERMES_AGENT_ID" not in env
    assert "LOOPSKILL_API_KEY" not in env  # existing cred scrub still applies
    assert env["GLM_API_KEY"] == "glm-keepme"
    entries = env["PATH"].split(os.pathsep)
    assert entries.count(node_dir) == 1
    for entry in entries:
        assert entry == node_dir or not run_mod._inside_real_home(entry)
    assert "/usr/local/bin" in entries and "/usr/bin" in entries
    assert str(HOME / ".local" / "bin") not in entries


def test_claude_cold_env_same_scrub(monkeypatch):
    """Contract 2 applies to the claude adapter too."""
    node_dir = str(HOME / ".hermes" / "node" / "bin")
    monkeypatch.setattr(run_mod.shutil, "which", _fake_which(node_dir))
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}{HOME / '.local' / 'bin'}")
    monkeypatch.setenv("HERMES_HOME", str(HOME / ".hermes"))
    env = run_mod.build_cold_env("claude")
    assert "HERMES_HOME" not in env
    assert str(HOME / ".local" / "bin") not in env["PATH"].split(os.pathsep)


def test_abs_binary_prefers_runner_lookup(monkeypatch):
    """Contract 4: absolutize against the runner env, fall back to the name."""
    monkeypatch.setattr(run_mod.shutil, "which", _fake_which("/x", codex="/opt/vendor/bin/codex"))
    assert run_mod._abs_binary("codex") == "/opt/vendor/bin/codex"
    assert run_mod._abs_binary("missing-harness") == "missing-harness"


def _capture_run(monkeypatch, stdout: str):
    seen = {}

    def _run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(run_mod.subprocess, "run", _run)
    return seen


def test_codex_adapter_launches_absolute_binary_with_scrubbed_env(monkeypatch, tmp_path):
    seen = _capture_run(monkeypatch, stdout="")
    node_dir = str(HOME / ".hermes" / "node" / "bin")
    monkeypatch.setattr(run_mod.shutil, "which", _fake_which(node_dir))
    monkeypatch.setenv("PATH", f"{HOME / '.local' / 'bin'}:/usr/bin")
    monkeypatch.setenv("HERMES_HOME", str(HOME / ".hermes"))
    monkeypatch.setattr(run_mod, "_copy_codex_oauth_credentials", lambda home: True)

    result = run_mod.run_codex_harness("find a marketplace", tmp_path, 1)

    assert seen["argv"][0] == "/opt/vendor/bin/codex"
    assert seen["argv"][1] == "exec"
    env = seen["env"]
    assert "HERMES_HOME" not in env
    for entry in env["PATH"].split(os.pathsep):
        assert entry == node_dir or not run_mod._inside_real_home(entry)
    assert result.tool_calls in (0, None)  # captured launch, empty transcript


def test_claude_adapter_launches_absolute_binary(monkeypatch, tmp_path):
    seen = _capture_run(monkeypatch, stdout="{}")
    monkeypatch.setattr(run_mod.shutil, "which", _fake_which("/x"))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(run_mod, "_copy_claude_oauth_credentials", lambda home, provider: True)

    run_mod.run_claude_harness("find a marketplace", tmp_path, 1)

    assert seen["argv"][0] == "/opt/vendor/bin/claude"
    assert seen["argv"][1] == "-p"
