"""Tests for repohygiene_2605 Phase A — stable signature generation in install_count_drift_probe.

Pins the contract that the same (slug, drift_kind) tuple ALWAYS hashes to the same
16-hex error_signature, regardless of:
  - which Python process generates it (PYTHONHASHSEED randomization)
  - which host
  - which clock time
  - which user

Pre-fix bug: `abs(hash((slug, 'install_count_drift'))) :016x` — Python's builtin
hash() is salt-randomized per-process when PYTHONHASHSEED=random (the default).
Result: every hourly cron run produced a brand-new signature for the same skill+drift,
the feedback-dispatcher had no dedup match, and a new GitHub issue was opened each
hour. 127 noise issues accumulated on wisechef-ai/recipes-api in ~5 days.

Fix: deterministic hashlib.sha256 of the canonical input.

Acceptance gate: Phase A in 2026-05-26-repohygiene-2605-execution-plan.md.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "scripts" / "install_count_drift_probe.py"


def _compute_signature_via_subprocess(slug: str, pyhashseed: str | None = None) -> str:
    """Import the probe module fresh in a subprocess and read its signature output.

    Using subprocess + explicit PYTHONHASHSEED is the only way to prove cross-run
    determinism — in-process import would inherit the current Python's hash seed.
    """
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        # Import the helper exposed by the probe module. Pre-fix the module computes
        # the signature inline inside report_drift; post-fix we expect a pure helper
        # `compute_signature(slug)` to exist.
        from scripts.install_count_drift_probe import compute_signature
        print(compute_signature({slug!r}))
        """
    )
    env = None
    if pyhashseed is not None:
        import os as _os

        env = {**_os.environ, "PYTHONHASHSEED": pyhashseed}
    out = subprocess.check_output(
        [sys.executable, "-c", code], env=env, text=True, stderr=subprocess.STDOUT
    )
    return out.strip()


def test_signature_is_16_hex_chars() -> None:
    """Signature must remain a 16-char lowercase hex string (matches dispatcher regex)."""
    sig = _compute_signature_via_subprocess("larry")
    assert len(sig) == 16, f"expected 16 chars, got {len(sig)}: {sig!r}"
    assert all(c in "0123456789abcdef" for c in sig), f"not hex: {sig!r}"


def test_signature_is_stable_across_python_hash_seeds() -> None:
    """Same slug + different PYTHONHASHSEED MUST produce the same signature.

    This is the regression test for the root cause: builtin hash() is salt-randomized
    per process, so a signature derived from hash() drifts across cron runs.
    Two subprocess runs with different PYTHONHASHSEED values prove the new
    implementation does NOT depend on Python's randomized hash.
    """
    sig_a = _compute_signature_via_subprocess("larry", pyhashseed="0")
    sig_b = _compute_signature_via_subprocess("larry", pyhashseed="42")
    sig_c = _compute_signature_via_subprocess("larry", pyhashseed="random")
    assert sig_a == sig_b == sig_c, (
        f"signature must be deterministic across PYTHONHASHSEED variants, "
        f"got: seed=0 -> {sig_a}, seed=42 -> {sig_b}, seed=random -> {sig_c}"
    )


def test_different_slugs_produce_different_signatures() -> None:
    """Sanity: distinct slugs must hash to distinct signatures (no all-zero constant)."""
    assert _compute_signature_via_subprocess("larry") != _compute_signature_via_subprocess(
        "pr-draft"
    )
    assert _compute_signature_via_subprocess("clean-architecture") != _compute_signature_via_subprocess(
        "code-review"
    )


def test_same_slug_same_signature_in_process() -> None:
    """compute_signature is a pure function of slug — repeated calls return same value."""
    from scripts.install_count_drift_probe import compute_signature

    assert compute_signature("larry") == compute_signature("larry")
    assert compute_signature("graphify") == compute_signature("graphify")


# ── Rate-limit guard (per plan §A step 5) ─────────────────────────────────


def test_rate_limit_marks_seen_and_blocks_within_24h(tmp_path) -> None:
    """`should_report(slug, sig, state_path, now)` returns True first time, False within 24h."""
    from scripts.install_count_drift_probe import should_report

    state = tmp_path / "drift-probe-seen.json"
    slug = "larry"
    sig = "deadbeefcafebabe"
    # First call: never seen — should report.
    assert should_report(slug, sig, state_path=state, now_ts=1_000_000.0) is True
    # Second call 1 hour later, same (slug, sig): rate-limited.
    assert should_report(slug, sig, state_path=state, now_ts=1_000_000.0 + 3600) is False
    # 25 hours later: window expired, may report again.
    assert should_report(slug, sig, state_path=state, now_ts=1_000_000.0 + 25 * 3600) is True


def test_rate_limit_does_not_block_different_slug(tmp_path) -> None:
    """Distinct slug or distinct signature MUST be allowed independently."""
    from scripts.install_count_drift_probe import should_report

    state = tmp_path / "drift-probe-seen.json"
    assert should_report("larry", "aaaa", state_path=state, now_ts=1_000_000.0) is True
    # Different slug: allowed.
    assert should_report("pr-draft", "aaaa", state_path=state, now_ts=1_000_000.0) is True
    # Different signature for same slug: allowed.
    assert should_report("larry", "bbbb", state_path=state, now_ts=1_000_000.0) is True
