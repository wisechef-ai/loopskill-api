"""bundles_0811 — the README's install line must work on a stranger's machine.

THE DEFECT
----------
P4's README opened with:

    pip install ./cli

Run from a real cold clone on this box's system Python:

    error: externally-managed-environment
    × This environment is externally managed

That is **PEP 668**, and it is the default on Debian, Ubuntu, Fedora and
Homebrew Python — i.e. most readers. The very first command of the cold-start
artifact failed, which silently voids P4's own gate ("a non-Adam reader reaches
a working `diff` from the README alone").

A subagent hit this during P2 verification and correctly called it an
environment property rather than a package defect. Both are true — and it is
still a broken first step for the reader, which is what the gate measures.

WHY `python3 -m venv` AND NOT `pipx`
------------------------------------
`pipx` is the other common answer, but it is not guaranteed present (it needed
its own install on several distros), and the README must not open by telling a
stranger to install a tool to install a tool. `venv` ships with CPython, so the
documented line depends on nothing the reader does not already have.

Verified on a fresh `git clone --depth 1` before this test was written:

    python3 -m venv .venv && ./.venv/bin/pip install ./cli
    ./.venv/bin/loopskill --version   ->  loopskill 0.2.0
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CLI_README = REPO_ROOT / "cli" / "README.md"


def _install_lines(text: str) -> list[str]:
    """Every line that installs the CLI package."""
    return [
        ln.strip()
        for ln in text.splitlines()
        if re.search(r"pip\s+install\b", ln) and ("./cli" in ln or re.search(r"-e\s+\.", ln))
    ]


class TestInstallLineSurvivesPEP668:
    def test_readme_does_not_open_with_a_bare_system_pip_install(self):
        """A bare `pip install ./cli` fails on any PEP 668 distro."""
        for line in _install_lines(README.read_text("utf-8")):
            assert not re.match(r"^pip\s+install\s+\./cli\s*(#.*)?$", line), (
                f"README install line {line!r} is a bare system-pip install and dies with "
                "'externally-managed-environment' on Debian/Ubuntu/Fedora/Homebrew Python. "
                "Use a venv (stdlib) so the first command works for a stranger."
            )

    def test_readme_creates_an_isolated_environment_first(self):
        text = README.read_text("utf-8")
        assert "python3 -m venv" in text, (
            "README must create an isolated environment before installing — "
            "venv is stdlib, so it adds no prerequisite the reader lacks"
        )

    def test_install_and_invocation_agree(self):
        """Installing into .venv but then calling a bare `loopskill` would 'work'
        only for a reader who already had one on PATH — the worst kind of doc bug."""
        text = README.read_text("utf-8")
        if "./.venv/bin/pip install" in text:
            assert "./.venv/bin/loopskill" in text or "alias loopskill=" in text, (
                "README installs into ./.venv but never shows how to invoke it from there"
            )

    def test_cli_readme_does_not_regress_either(self):
        """cli/README.md documents `pip install -e .` for contributors.

        That is legitimate — a contributor is expected to be in a virtualenv —
        but it must SAY so rather than assume it, since the same PEP 668 wall
        is waiting.
        """
        text = CLI_README.read_text("utf-8")
        if _install_lines(text):
            assert re.search(r"venv|virtualenv|isolated environment", text, re.IGNORECASE), (
                "cli/README.md documents a pip install but never mentions a virtual "
                "environment — readers on PEP 668 distros hit externally-managed-environment"
            )
