"""mesh_0408 W5 — the moat-loop proof HARNESS must itself stay falsifiable.

Trap V4: a verification harness can be broken. A probe once did a ``str.replace``
that missed its anchor, mutated nothing, and reported PASS. So the proof script's
own failure branches are exercised here, in CI, on every run — not once by hand.

What this pins:
  - ``selftest()`` passes, i.e. every scenario in SELFTEST_MATRIX lands on the
    exit code it claims
  - exits {0, 1, 2} are ALL reachable — a script that can only ever return 0 is
    decoration
  - the matrix actually discriminates: break one assertion in run_proof and a
    specific scenario must go red (the RED-proof, automated)
  - the payload varies per run (trap E3) — a fixed feedback message would stay
    pinned to a previously-failed dedup row forever

The script is imported by path because ``scripts/`` is not a package on the
test path; that is also a guard in itself — if the file is renamed or moved, this
test fails rather than silently skipping.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "moat_loop_proof.py"


def _load(name: str = "moat_loop_proof"):
    assert _SCRIPT.is_file(), f"the proof script has moved or been deleted: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: @dataclass resolves `field(default_factory=...)`
    # through sys.modules[cls.__module__], which is None for an unregistered
    # spec-loaded module.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def proof():
    return _load()


def test_selftest_passes(proof, capsys):
    """The harness falsification run must succeed."""
    assert proof.selftest() == proof.EXIT_PROVEN
    out = capsys.readouterr().out
    assert "harness falsified" in out


def test_all_three_exit_codes_are_reachable(proof):
    """0 / 1 / 2 must each be produced by some real scenario.

    A proof script whose VOID branch is unreachable cannot tell "the loop works"
    apart from "I could not check", which is precisely the failure mode exit 2
    exists to prevent.
    """
    reached = set()
    for _name, _want, _step, overrides in proof.SELFTEST_MATRIX:
        out = proof.run_proof(
            proof._StubTransport(**overrides),
            skill="s",
            bundle_id="bundle-1",
            bundle_slug="b",
            repo="o/private-sink",
            log=lambda _m: None,
        )
        reached.add(out.code)
    assert reached == {proof.EXIT_PROVEN, proof.EXIT_FAILED, proof.EXIT_VOID}, (
        f"exit codes reachable: {sorted(reached)} — every branch must be demonstrable"
    )


def test_every_step_has_a_failing_scenario(proof):
    """Each of the 5 steps must be able to fail. A step with no red scenario is
    a step nobody has ever seen go wrong — i.e. an unverified one."""
    failing_steps = {
        step for _n, code, step, _o in proof.SELFTEST_MATRIX if code == proof.EXIT_FAILED and step
    }
    assert failing_steps == set(proof.STEPS), (
        f"steps with no failure scenario: {sorted(set(proof.STEPS) - failing_steps)}"
    )


def test_matrix_discriminates_when_the_assertion_is_broken(proof, monkeypatch):
    """RED-proof, automated: neuter the terminal-state assertion and the
    'member never reaches a terminal state' scenario MUST stop being caught.

    Without this, the matrix could be green because the scenarios are toothless
    rather than because the checks work.
    """
    src = _SCRIPT.read_text()
    anchor = 'if reported.get("status") != "converged" or not reported.get("terminal"):'
    assert anchor in src, "anchor moved — this RED-proof is no longer testing what it claims"
    mutated_src = src.replace(anchor, "if False:")
    assert mutated_src != src, "mutation changed no bytes"

    import types

    mutated = types.ModuleType("moat_loop_proof_mutated")
    mutated.__file__ = str(_SCRIPT)
    sys.modules["moat_loop_proof_mutated"] = mutated  # see _load(): dataclass needs this
    exec(compile(mutated_src, str(_SCRIPT), "exec"), mutated.__dict__)

    overrides = {"report": (200, {"status": "applying", "terminal": False})}
    before = proof.run_proof(
        proof._StubTransport(**overrides),
        skill="s",
        bundle_id="bundle-1",
        bundle_slug="b",
        repo="o/private-sink",
        log=lambda _m: None,
    )
    assert before.code == proof.EXIT_FAILED, "the intact harness must catch a non-terminal job"

    after = mutated.run_proof(
        mutated._StubTransport(**overrides),
        skill="s",
        bundle_id="bundle-1",
        bundle_slug="b",
        repo="o/private-sink",
        log=lambda _m: None,
    )
    assert after.code != proof.EXIT_FAILED, (
        "removing the terminal-state assertion changed nothing — the scenario was "
        "being caught by some OTHER check, so it does not test what it says it does"
    )


def test_feedback_payload_varies_between_runs(proof):
    """Trap E3 — feedback dedups on a signature derived from the message text."""
    messages = set()
    for _ in range(3):
        stub = proof._StubTransport()
        proof.run_proof(
            stub,
            skill="s",
            bundle_id="bundle-1",
            bundle_slug="b",
            repo="o/private-sink",
            log=lambda _m: None,
        )
        messages.add(stub.last_message)
    assert len(messages) == 3, "a fixed payload would pin this proof to a stale dedup row forever"


def test_deduped_feedback_is_treated_as_a_failure(proof):
    """A deduped response echoes a PREVIOUS run's issue_url. Accepting it would
    let a broken rail report green off a historic success."""
    out = proof.run_proof(
        proof._StubTransport(
            feedback={"ok": True, "issue_url": "https://github.com/o/r/issues/1", "deduped": True}
        ),
        skill="s",
        bundle_id="bundle-1",
        bundle_slug="b",
        repo="o/private-sink",
        log=lambda _m: None,
    )
    assert out.code == proof.EXIT_FAILED
    assert out.step == proof.STEPS[1]


def test_void_is_never_reported_as_success(proof):
    """Exit 2 must be distinct from exit 0 in both code and wording."""
    out = proof.run_proof(
        proof._StubTransport(bundle_id_row=proof.Void("DATABASE_URL is not set")),
        skill="s",
        bundle_id="bundle-1",
        bundle_slug="b",
        repo="o/private-sink",
        log=lambda _m: None,
    )
    assert out.code == proof.EXIT_VOID
    assert out.code != proof.EXIT_PROVEN
