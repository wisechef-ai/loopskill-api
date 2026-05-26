"""
Phase H3 — feedback-dispatcher.yml storm-test predicate contract test.

Verifies that the recipify-request handler in the dispatcher:
  1. Contains a 'tester' predicate (the correct narrow match).
  2. Does NOT use a bare 'test' string as the predicate (too broad — would
     match legitimate usernames like 'test-account-1'; see repohygiene_2605 §6 risk #12).

This is an intentionally lightweight structural test: it pins the contract
that the predicate exists and is correctly scoped, without spinning up GH
Actions infrastructure.
"""

import re
from pathlib import Path

DISPATCHER_PATH = Path(__file__).parent.parent / ".github" / "workflows" / "feedback-dispatcher.yml"


def _extract_recipify_script(dispatcher_text: str) -> str:
    """
    Extract the JS block that belongs to the recipify-request handler.

    The dispatcher is a multi-event if/else chain inside a single `script: |`
    YAML block.  We isolate the recipify-request branch by slicing from
    "eventType === 'recipify-request'" to the next "} else if" / "} else {"
    guard so we don't accidentally match lines from other branches.
    """
    marker = "eventType === 'recipify-request'"
    start = dispatcher_text.find(marker)
    assert start != -1, f"Could not find recipify-request handler in {DISPATCHER_PATH}"

    # Walk forward to the next top-level } else clause (a sibling branch).
    # We look for "} else if" that is NOT inside the recipify block itself.
    # A simple heuristic: find the next occurrence after start.
    sibling_pattern = re.compile(r"\}\s*else\s+(?:if|{)")
    tail = dispatcher_text[start:]
    match = sibling_pattern.search(tail, 1)  # skip the '=== recipify' line itself
    if match:
        return tail[: match.start()]
    # No sibling found — return remainder of script.
    return tail


def test_dispatcher_file_exists() -> None:
    assert DISPATCHER_PATH.exists(), f"Dispatcher YAML not found at {DISPATCHER_PATH}"


def test_storm_predicate_uses_tester_keyword() -> None:
    """The recipify-request handler must contain 'tester' as the predicate keyword."""
    text = DISPATCHER_PATH.read_text()
    recipify_block = _extract_recipify_script(text)
    assert "'tester'" in recipify_block, (
        "Expected `includes('tester')` predicate in the recipify-request handler "
        "of feedback-dispatcher.yml, but 'tester' was not found in that block.\n"
        f"Block content:\n{recipify_block[:600]}"
    )


def test_storm_predicate_does_not_use_bare_test() -> None:
    """
    Regression: the predicate must NOT use the bare string 'test' as the
    includes() argument (too broad — matches legitimate usernames).

    We check that includes('test') does NOT appear inside the recipify-request
    block.  Note: includes('tester') is fine and should still pass because
    the preceding test already pins that.
    """
    text = DISPATCHER_PATH.read_text()
    recipify_block = _extract_recipify_script(text)
    # "includes('tester')" is allowed; "includes('test')" (bare) is not.
    bare_test_pattern = re.compile(r"includes\(\s*['\"]test['\"]\s*\)")
    assert not bare_test_pattern.search(recipify_block), (
        "Found `includes('test')` (bare) in the recipify-request handler — "
        "this is too broad and would match real usernames like 'test-account-1'. "
        "Use `includes('tester')` instead (repohygiene_2605 §6 risk #12)."
    )


def test_storm_test_artifact_label_referenced() -> None:
    """The recipify-request handler must push the 'storm-test-artifact' label."""
    text = DISPATCHER_PATH.read_text()
    recipify_block = _extract_recipify_script(text)
    assert "storm-test-artifact" in recipify_block, (
        "Expected 'storm-test-artifact' label to be referenced in the "
        "recipify-request handler of feedback-dispatcher.yml."
    )
