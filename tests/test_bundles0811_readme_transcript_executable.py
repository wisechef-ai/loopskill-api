"""bundles_0811 — the README's "literal transcript" must actually be literal.

THE GAP
-------
README.md opens with the cold-start artifact and makes an unusually strong,
unusually falsifiable promise:

    This is a literal, self-contained transcript. Paste every command below
    into a clean shell ... You will get *exactly* this output.

Nothing verified it. `tests/test_readme_claims.py` is the README guard, but it
protects PROSE — its `_strip_fenced_code()` deliberately removes fenced blocks
before every assertion, so the transcript is the one region of the README that
its own guard is blind to by construction. The headline claim of the cold-start
artifact was therefore the single least-protected sentence in the file.

That matters more here than for an ordinary doc drift:

* This block IS the cold-start funnel. A stranger's first 60 seconds with the
  product is this paste. If the output has drifted, the reader concludes the
  tool is broken — there is no second impression.
* The promise is *exactness*, not vibes. A one-word change to any CLI summary
  line ("DRIFT DETECTED", "in sync", "wrote ... skill(s)") silently falsifies a
  documented guarantee while every other test in the repo stays green.
* The set of scanned clients is baked into the expected output (claude / codex /
  cursor / hermes). Adding a client — a routine, desirable change — rewrites the
  transcript. That should fail loudly here, not surprise a reader.

WHAT THIS TEST DOES
-------------------
Extracts the two fenced blocks (commands, then expected output) straight from
README.md, EXECUTES the commands in an isolated tmp dir against the CLI built
from this checkout, and asserts the real stdout equals the documented output.

The commands are not re-implemented here — they are read from the README, so
the test cannot drift away from the thing it is checking. If someone edits the
transcript, this test runs the NEW commands and demands the NEW output be true.

Deliberately NOT asserted: wall-clock ("60 seconds"), and anything about the
network. `import`/`diff` are documented as making no network call, and the test
runs them against a fabricated `--home`, so a sandbox without egress still
passes.

Verified against a real cold clone (7840b99) before this test was written:
    git clone ... && python3 -m venv .venv && ./.venv/bin/pip install ./cli
    -> loopskill 0.2.0, transcript reproduced byte-for-byte, `diff` exits 1.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# The transcript's own framing sentence. If this wording goes away the promise
# is no longer being made and this test should be re-scoped, not silently pass.
PROMISE_RE = re.compile(r"literal,\s*self-contained transcript", re.IGNORECASE)

FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    """[(language, body)] for every fenced block, in document order."""
    return [(m.group(1), m.group(2)) for m in FENCE_RE.finditer(text)]


def _transcript_blocks(text: str) -> tuple[str, str]:
    """Return (commands, expected_output) — the first ```sh block and the
    unlabelled block that immediately follows it."""
    blocks = _fenced_blocks(text)
    for i, (lang, body) in enumerate(blocks):
        if lang == "sh" and "loopskill import" in body:
            assert i + 1 < len(blocks), (
                "README transcript: the ```sh command block is not followed by "
                "an output block — the promised output is missing."
            )
            nxt_lang, nxt_body = blocks[i + 1]
            assert nxt_lang == "", (
                "README transcript: the block after the commands is fenced as "
                f"{nxt_lang!r}; the expected-output block must be unlabelled."
            )
            return body, nxt_body
    pytest.fail("README transcript: no ```sh block containing 'loopskill import'")


def _rewrite_for_local_cli(commands: str, cli_bin: Path, demo_root: Path) -> str:
    """Point the documented commands at THIS checkout's CLI and a private tmp dir.

    Only two substitutions are made, and both are environment plumbing rather
    than behaviour:
      * the clone + venv + alias preamble -> the already-built cli_bin
      * /tmp/loopskill-demo -> a per-run temp dir (so a developer's leftover
        demo dir, or a parallel test run, cannot make this pass or fail)
    Every other character of the documented commands is executed as written.
    """
    out = []
    for line in commands.splitlines():
        stripped = line.strip()
        if stripped.startswith(("git clone", "python3 -m venv", "alias loopskill")):
            continue
        if stripped.startswith("loopskill "):
            line = line.replace("loopskill ", f"{cli_bin} ", 1)
        out.append(line)
    body = "\n".join(out)
    return body.replace("/tmp/loopskill-demo", str(demo_root))


def _normalise(text: str, demo_root: Path) -> str:
    """Map the run's temp paths back onto the documented ones and trim trailing
    whitespace per line, so only meaningful differences survive."""
    text = text.replace(str(demo_root), "/tmp/loopskill-demo")
    return "\n".join(ln.rstrip() for ln in text.strip().splitlines())


def test_readme_still_promises_a_literal_transcript() -> None:
    """The guarantee itself must be present — otherwise this file is testing
    a promise nobody makes any more, and would pass vacuously."""
    assert PROMISE_RE.search(README.read_text(encoding="utf-8")), (
        "README.md no longer claims a 'literal, self-contained transcript'. "
        "If the cold-start section was intentionally reworded, update or remove "
        "this test deliberately rather than leaving it to pass on nothing."
    )


def test_transcript_block_is_well_formed() -> None:
    """Cheap structural check that runs even where the CLI cannot be built."""
    commands, expected = _transcript_blocks(README.read_text(encoding="utf-8"))
    assert "loopskill import" in commands
    assert "loopskill diff" in commands
    assert expected.strip(), "the expected-output block is empty"
    # The documented output names the scanned clients; keep that concrete so a
    # new client cannot be added without the transcript being re-recorded.
    assert "[claude]" in expected, "expected output no longer mentions [claude] — re-record the transcript"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_readme_transcript_reproduces_exactly(tmp_path: Path) -> None:
    """Execute the README's own commands; demand its own documented output."""
    commands, expected = _transcript_blocks(README.read_text(encoding="utf-8"))

    # Build the CLI from THIS checkout into an isolated venv. If the package
    # cannot be built here (no network for build deps, etc.) skip rather than
    # report a false failure — the claim under test is about output, not pip.
    venv = tmp_path / "venv"
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=300
        )
        pip = venv / "bin" / "pip"
        subprocess.run(
            [str(pip), "install", "--quiet", str(REPO_ROOT / "cli")],
            check=True,
            capture_output=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not build the CLI in this environment: {exc}")

    cli_bin = venv / "bin" / "loopskill"
    assert cli_bin.exists(), "the cli package did not install a 'loopskill' entry point"

    demo_root = tmp_path / "demo"
    script = _rewrite_for_local_cli(commands, cli_bin, demo_root)

    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=300, cwd=str(tmp_path)
    )

    # `loopskill diff` exits 1 BY DESIGN when drift is found — the README says
    # so explicitly. The transcript ends on a drift, so a 0 here would mean the
    # documented script-and-CI-friendly exit contract had regressed.
    assert proc.returncode == 1, (
        "the transcript ends in a DRIFT FOUND state, which README.md documents "
        f"as exit code 1; got {proc.returncode}.\nstderr:\n{proc.stderr}"
    )

    actual = _normalise(proc.stdout, demo_root)
    want = _normalise(expected, demo_root)
    assert actual == want, (
        "README.md's cold-start transcript no longer reproduces.\n"
        "It promises the reader *exactly* this output.\n\n"
        f"--- documented ---\n{want}\n\n--- actual ---\n{actual}\n"
    )
