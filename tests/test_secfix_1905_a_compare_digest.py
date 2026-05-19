"""secfix_1905 Phase A — Issue #3: Master key timing-safe comparison.

Source-grep regression tests asserting middleware.py uses hmac.compare_digest
instead of == for both:
  1. Master key comparison (key == settings.API_KEY → timing oracle)
  2. Per-row token_hash comparison (match.token_hash == key_hash → timing oracle)

PoV tests FAIL on main (== is used). Pass after fix.
"""
import ast
import re
from pathlib import Path


MIDDLEWARE_PATH = Path(__file__).parent.parent / "app" / "middleware.py"


# ── PoV: verify == is NOT used for secret comparisons (FAILS on main) ────────

def test_pov_middleware_does_not_use_equals_for_api_key():
    """PROOF OF VULNERABILITY: middleware.py should NOT use == to compare
    the master API_KEY. On unfixed main, it does, creating a timing oracle.

    Expected: FAIL on main (== present)
    Expected: PASS after fix (hmac.compare_digest used)
    """
    source = MIDDLEWARE_PATH.read_text()
    # Check that hmac.compare_digest is used (not ==) for the master key check
    assert "hmac.compare_digest" in source, (
        "middleware.py must use hmac.compare_digest for secret comparisons. "
        "Using == leaks timing information that can be exploited to guess the master key."
    )


def test_pov_middleware_uses_compare_digest_at_least_twice():
    """PROOF OF VULNERABILITY: Two secret comparisons in middleware.py
    (master key at line ~283, token_hash at line ~251) must both use
    hmac.compare_digest.

    Expected: FAIL on main (only 0 or 1 occurrence)
    Expected: PASS after fix (≥2 occurrences)
    """
    source = MIDDLEWARE_PATH.read_text()
    count = source.count("hmac.compare_digest")
    assert count >= 2, (
        f"Expected ≥2 uses of hmac.compare_digest in middleware.py, found {count}. "
        f"Both the master key comparison AND the per-row token_hash comparison "
        f"must use timing-safe comparison."
    )


def test_pov_middleware_imports_hmac():
    """PROOF OF VULNERABILITY: middleware.py must import hmac.

    Expected: FAIL on main (no import)
    Expected: PASS after fix
    """
    source = MIDDLEWARE_PATH.read_text()
    assert re.search(r"^import hmac", source, re.MULTILINE), (
        "middleware.py must import hmac at the top level for hmac.compare_digest."
    )


# ── Negative: ensure == is NOT used for secret comparisons ────────────────────

def test_middleware_no_bare_equals_for_secrets():
    """After fix: no bare == comparison against settings.API_KEY or token_hash.

    This is a regression guard — if someone reverts to == accidentally, this fails.
    """
    source = MIDDLEWARE_PATH.read_text()
    # Check there is no `key == settings.API_KEY` pattern
    assert "key == settings.API_KEY" not in source, (
        "middleware.py still has 'key == settings.API_KEY' timing-oracle pattern. "
        "Use hmac.compare_digest(key, settings.API_KEY) instead."
    )
    # Check there is no `match.token_hash == key_hash` pattern
    assert "match.token_hash == key_hash" not in source, (
        "middleware.py still has 'match.token_hash == key_hash' timing-oracle pattern. "
        "Use hmac.compare_digest(match.token_hash, key_hash) instead."
    )
    # Check there is no `row.token_hash == key_hash` variant
    assert "row.token_hash == key_hash" not in source, (
        "middleware.py still has 'row.token_hash == key_hash' timing-oracle pattern."
    )
