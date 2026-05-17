"""Dispatcher dedup workflow regression — issue #59.

The feedback dispatcher previously used ``octokit.rest.search.issuesAndPullRequests``
to dedupe near-simultaneous submissions. That endpoint hits GitHub's search
INDEX, which lags by several seconds, so two submissions with the same
``error_signature`` within ~2s reliably opened TWO duplicate issues (e.g.
#51 + #52 on 2026-05-08, both reporting super-memory error_signature
``99f3eab4...``).

These tests pin the workflow file's structure so a future "simplify" can't
silently regress to the broken behaviour. We assert:

- ``issues.listForRepo`` is the primary path (no search-index lag).
- The legacy ``search.issuesAndPullRequests`` call is retained ONLY as a
  fallback inside the catch block.
- The dedup logic looks for ``error_signature`` in title, ``dedup_hash``
  in body, and the catch-all ``signature`` in body.
- The label filter is event-type-aware (``feedback`` / ``recipe:request``
  / ``recipe:bug``) so the realtime list is scoped.
"""

from __future__ import annotations

from pathlib import Path

import pytest


WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github" / "workflows" / "feedback-dispatcher.yml"
)


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_uses_listforrepo_for_realtime_dedup(workflow_text):
    """The primary dedup path MUST use listForRepo, not the search index."""
    assert "octokit.rest.issues.listForRepo" in workflow_text, (
        "regression: dispatcher no longer calls issues.listForRepo for "
        "realtime dedup — see issue #59"
    )
    # And it must be inside the dedup block (not somewhere unrelated).
    dedup_section_start = workflow_text.index("Dedup: search recent open issues by signature")
    dedup_section_end = workflow_text.index("Ensure category sub-labels", dedup_section_start)
    section = workflow_text[dedup_section_start:dedup_section_end]
    assert "octokit.rest.issues.listForRepo" in section


def test_workflow_retains_search_index_only_as_fallback(workflow_text):
    """search.issuesAndPullRequests should appear only inside catch blocks
    (fallbacks). The fix replaces TWO realtime calls (PR dedup at line ~88
    and issue dedup at line ~340) with the realtime alternative; both
    fallbacks remain for resilience."""
    # Count only actual function calls (not comment references). The call
    # shape is octokit.rest.search.issuesAndPullRequests({ ... }) — match by
    # the dotted-path prefix followed by an opening paren.
    import re as _re
    calls = _re.findall(
        r"octokit\.rest\.search\.issuesAndPullRequests\s*\(",
        workflow_text,
    )
    # Two fallback calls — one in skill-patch PR dedup, one in skill-error
    # issue dedup. Both must live inside catch blocks.
    assert len(calls) == 2, (
        f"expected exactly 2 octokit.rest.search.issuesAndPullRequests calls "
        f"(both as fallbacks), got {len(calls)}. Issue #59 fix may have regressed."
    )

    # Each call must be inside a fallback / catch handler — not the primary
    # dedup path. Anchor on the catch's first message line "Realtime ... failed"
    # within the preceding 1500 chars.
    for m in _re.finditer(r"octokit\.rest\.search\.issuesAndPullRequests", workflow_text):
        idx = m.start()
        window = workflow_text[max(0, idx - 1500): idx]
        assert "realtime" in window.lower() and "failed" in window.lower(), (
            f"octokit.rest.search.issuesAndPullRequests at offset {idx} is "
            f"not preceded by a realtime-failure catch handler — the dedup "
            f"fix may have regressed."
        )


def test_workflow_pr_dedup_uses_realtime_pulls_list(workflow_text):
    """The skill-patch PR dedup path (top of file) must also use the
    realtime pulls.list, not search-index."""
    # Anchor on the comment that lives only on the PR dedup path.
    pr_block_start = workflow_text.index("Dedup: look for an existing open PR")
    pr_block_end = workflow_text.index("if (existingPR)", pr_block_start)
    pr_block = workflow_text[pr_block_start:pr_block_end]
    assert "octokit.rest.pulls.list" in pr_block, (
        "regression: skill-patch PR dedup no longer uses pulls.list — see #59"
    )


def test_workflow_dedup_handles_three_signal_types(workflow_text):
    """All three dedup signals must be checked: error_signature, dedup_hash,
    and signature. Dropping any of these breaks one of the event types."""
    for needle in (
        "error_signature",
        "dedup_hash",
        "iss.title.includes(errorSig)",
        "body.includes(dedupHash)",
        "body.includes(signature)",
    ):
        assert needle in workflow_text, f"dedup signal {needle!r} missing"


def test_workflow_dedup_label_filter_is_event_type_aware(workflow_text):
    """Label filter must scope the listForRepo call by event-type label so
    the realtime list doesn't fetch 100 unrelated issues."""
    for label in ("'feedback'", "'recipe:request'", "'recipe:bug'"):
        assert label in workflow_text, f"event-type label {label} missing from dedup"
    # And the filter must actually be passed to listForRepo.
    assert "labels: dedupLabel" in workflow_text


def test_workflow_fallback_does_not_use_hardcoded_org_name(workflow_text):
    """Avoid hardcoding the org/repo name in the fallback query — the original
    bug also baked in 'wisechef-ai/recipes-api' which would break if the repo
    moved. The fallback now uses context.repo.{owner,repo}."""
    # The original hardcoded query must be gone.
    assert "repo:wisechef-ai/recipes-api" not in workflow_text, (
        "regression: dispatcher dedup still hardcodes 'wisechef-ai/recipes-api' "
        "in the fallback query"
    )
    # Replacement uses context.repo bindings.
    assert "context.repo.owner" in workflow_text
    assert "context.repo.repo" in workflow_text


def test_workflow_dedup_path_failures_are_non_fatal(workflow_text):
    """If listForRepo and the fallback both fail, the dispatcher must NOT
    bail — it should still open the new issue. Otherwise a transient GitHub
    API hiccup silently swallows agent feedback."""
    # Both wrapping try blocks must use core.warning (non-fatal), never throw.
    assert "Realtime dedup failed (non-fatal)" in workflow_text
    assert "Search-index fallback also failed" in workflow_text


def test_node_smoke_test_passes():
    """Run the Node smoke test that exercises the dedup logic against a
    mocked octokit. If this test is skipped or fails, the regression guard
    is meaningless — verifies actual JS behaviour, not just static structure."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available — install node 18+ to run JS smoke tests")

    smoke = (
        Path(__file__).resolve().parent
        / "feedback_dispatcher_dedup.smoke.js"
    )
    assert smoke.is_file(), "smoke test script missing"
    r = subprocess.run(
        [node, str(smoke)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, (
        f"smoke test failed (exit {r.returncode}):\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "All smoke assertions passed" in r.stdout
