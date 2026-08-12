"""The dangling-reference gate is WIRED INTO THE PUBLISH PATH, not just tested.

`app/services/skill_refs.py` shipped 2026-08-08 (#208) with a docstring calling
itself "the dangling-reference publishing gate". It was imported by six unit
tests and one manual script that lives in no cron and no CI workflow — and by
NOTHING in the request path. So the service was correct, well-tested, and had
never once prevented a 404.

Measured on the live catalog 2026-08-12: 7 dangling references across 5
published skills, reproduced independently by two implementations (this
service, and the fleet's `fdeloop0808-frontdoor` predicate) agreeing exactly.

These tests exist to keep the WIRING, which is the part that was missing. A
test that only exercises `find_dangling_references()` directly would have
passed every day of the four days the gate was dead.
"""

from __future__ import annotations

import pytest


def _find_publish_module():
    from app import publisher_routes

    return publisher_routes


class TestGateIsActuallyWired:
    """The regression that mattered: the service imported but never called."""

    def test_publisher_module_imports_the_service(self):
        pr = _find_publish_module()
        assert hasattr(pr, "find_dangling_references"), (
            "publisher_routes no longer imports find_dangling_references — "
            "the gate has been unwired and publishes can emit 404 references again"
        )

    def test_publish_route_body_calls_the_service(self):
        """Import alone is not wiring; the request path must invoke it."""
        import inspect

        pr = _find_publish_module()
        src = inspect.getsource(pr.publish_skill)
        assert "find_dangling_references(" in src, (
            "publish_skill() does not call find_dangling_references — an imported "
            "but uncalled gate is exactly the state this test was written to prevent"
        )

    def test_findings_reach_the_publisher_not_only_the_log(self):
        import inspect

        pr = _find_publish_module()
        src = inspect.getsource(pr.publish_skill)
        assert "dangling_refs" in src and "warnings.append" in src, (
            "dangling references must be appended to the response warnings; a log "
            "line the publisher never reads is not a notification"
        )

    def test_gate_is_advisory_not_blocking(self):
        """Deliberate: 7 pre-existing dangles must not brick unrelated republishes."""
        import inspect

        pr = _find_publish_module()
        src = inspect.getsource(pr.publish_skill)
        gate = src.split("DANGLING-REFERENCE GATE")[1].split("Un-archive")[0]
        assert "HTTPException" not in gate, (
            "the dangling-reference gate must WARN, not raise: the tarball is already "
            "stored and the version row already exists at that point, so raising "
            "leaves a half-published skill"
        )

    def test_gate_failure_cannot_break_a_publish(self):
        import inspect

        pr = _find_publish_module()
        src = inspect.getsource(pr.publish_skill)
        gate = src.split("DANGLING-REFERENCE GATE")[1].split("Un-archive")[0]
        assert "except Exception" in gate, (
            "the gate must swallow its own failures — an unavailable check is "
            "reported, never fatal to the publish"
        )


class TestDetectionSemantics:
    """The behaviour the wiring depends on. Anchored to the REAL live defects."""

    def test_the_seven_live_dangles_are_detected(self):
        """Verbatim from the 2026-08-12 catalog audit."""
        from app.services.skill_refs import find_dangling_references

        readmes: dict[str, str | None] = {
            "clean-code": "For refactoring techniques, see refactoring-patterns. "
                          "see `karpathy-coding-defaults` principle 3.",
            "domain-driven-design": "For complexity, see software-design-philosophy.",
            "github-issues": "- Authenticated with GitHub (see `github-auth` skill)",
            "hundred-million-offers": "For product positioning, see obviously-awesome. "
                                      "For outbound sales, see predictable-revenue.",
            "ollama-low-vram-model-pick": "see `cognee-retrieval-architecture` \"Embedding\"",
        }
        published = set(readmes)  # the 5 sources are published; the 7 targets are not
        out = find_dangling_references(readmes, published)
        assert out["clean-code"] == {"refactoring-patterns", "karpathy-coding-defaults"}
        assert out["domain-driven-design"] == {"software-design-philosophy"}
        assert out["github-issues"] == {"github-auth"}
        assert out["hundred-million-offers"] == {"obviously-awesome", "predictable-revenue"}
        assert out["ollama-low-vram-model-pick"] == {"cognee-retrieval-architecture"}
        assert sum(len(v) for v in out.values()) == 7

    def test_resolving_references_are_silent(self):
        from app.services.skill_refs import find_dangling_references

        readmes: dict[str, str | None] = {"a-skill": "see b-skill for more"}
        assert find_dangling_references(readmes, {"a-skill", "b-skill"}) == {}

    def test_self_reference_is_never_dangling(self):
        from app.services.skill_refs import find_dangling_references

        assert find_dangling_references({"a-skill": "see a-skill above"}, set()) == {}

    def test_code_fences_are_examples_not_claims(self):
        from app.services.skill_refs import find_dangling_references

        readmes: dict[str, str | None] = {"a-skill": "```\nsee not-a-real-skill\n```"}
        assert find_dangling_references(readmes, {"a-skill"}) == {}

    @pytest.mark.parametrize("prose", ["see also", "see below", "see https://example.com/x"])
    def test_prose_see_does_not_manufacture_a_reference(self, prose):
        """A false accusation against working copy is worse than a missed 404."""
        from app.services.skill_refs import find_dangling_references

        assert find_dangling_references({"a-skill": prose}, {"a-skill"}) == {}
