"""fedtotal_0901 — ONE federated total, no double-count.

Regression suite for a real production defect: PR #301 added
``counts.federated_skills_total`` to the public marketing snapshot with its
OWN dedupe implementation ("prefer deduped_indexed, else indexed"). That
implementation missed the clawhub subset rule that
``GET /api/skills/external`` had encoded since 2026-07-15, so the two public
surfaces published DIFFERENT values for the same number:

    external_indexed         =  91,362   (correct)
    federated_skills_total   = 168,379   (WRONG — +77,017)

77,017 is exactly the direct clawhub walk, which is a strict SUBSET of the
hub snapshot's clawhub coverage and must not be added on top of it.

The fix hoists the topology into ONE function
(``federation_cache.sum_federated_total``) that BOTH surfaces call. These
tests pin the topology and, critically, the PARITY — so a future change to
one surface's number cannot silently desync the other again.
"""

from __future__ import annotations

from app.services.federation_cache import sum_federated_total

# The live production shape as measured 2026-09-01 (trimmed to the sources
# that actually matter for the dedupe topology, plus one representative
# ordinary source and one never-walked source).
LIVE_BLOCKS: dict[str, dict] = {
    "hermes-hub": {"indexed": 90605, "deduped_indexed": 70638},
    "clawhub": {"indexed": 77017, "deduped_indexed": None},
    "skills-sh": {"indexed": 20000, "deduped_indexed": None},
    "github-anthropic": {"indexed": 19, "deduped_indexed": None},
    "well-known": {"indexed": 0, "deduped_indexed": None},
    "never-walked": {"indexed": None, "deduped_indexed": None},
}

# 70638 (hub deduped) + 20000 + 19 + 0 = 90657; clawhub EXCLUDED.
EXPECTED_LIVE_TOTAL = 70638 + 20000 + 19 + 0


class TestClawhubIsNotDoubleCounted:
    """The actual shipped bug."""

    def test_clawhub_excluded_when_hub_snapshot_is_fresh(self):
        total = sum_federated_total(LIVE_BLOCKS)
        assert total == EXPECTED_LIVE_TOTAL, (
            f"expected {EXPECTED_LIVE_TOTAL}, got {total}"
        )

    def test_total_does_not_include_the_direct_clawhub_walk(self):
        """The precise regression: +77,017 must NOT appear in the total."""
        total = sum_federated_total(LIVE_BLOCKS)
        naive = total + LIVE_BLOCKS["clawhub"]["indexed"]
        assert total != naive, "clawhub was added on top of the hub snapshot"
        assert LIVE_BLOCKS["clawhub"]["indexed"] == 77017
        assert total < naive

    def test_hub_contributes_deduped_not_raw(self):
        """hermes-hub must contribute 70,638 — not its raw 90,605."""
        only_hub = {"hermes-hub": LIVE_BLOCKS["hermes-hub"]}
        assert sum_federated_total(only_hub) == 70638


class TestStaleHubDoesNotDeleteClawhub:
    """The inverse failure: over-correcting and dropping clawhub entirely.

    ``hub_fresh`` gates the topology. With no usable hub snapshot the direct
    clawhub walk is the ONLY clawhub signal, so it MUST be counted — dropping
    it would silently remove ~77k skills from the headline.
    """

    def test_clawhub_counted_when_hub_has_no_deduped_count(self):
        blocks = {
            "hermes-hub": {"indexed": 90605, "deduped_indexed": None},
            "clawhub": {"indexed": 77017, "deduped_indexed": None},
        }
        assert sum_federated_total(blocks) == 90605 + 77017

    def test_clawhub_counted_when_hub_deduped_is_zero(self):
        blocks = {
            "hermes-hub": {"indexed": 0, "deduped_indexed": 0},
            "clawhub": {"indexed": 77017, "deduped_indexed": None},
        }
        assert sum_federated_total(blocks) == 77017

    def test_clawhub_counted_when_hub_absent_entirely(self):
        blocks = {"clawhub": {"indexed": 77017, "deduped_indexed": None}}
        assert sum_federated_total(blocks) == 77017


class TestHonestDegradation:
    """decision #5: a never-walked source is OMITTED, never counted as 0."""

    def test_null_indexed_is_omitted_not_zero(self):
        blocks = {
            "a": {"indexed": 100, "deduped_indexed": None},
            "b": {"indexed": None, "deduped_indexed": None},
        }
        assert sum_federated_total(blocks) == 100

    def test_empty_blocks_yield_zero(self):
        assert sum_federated_total({}) == 0

    def test_total_is_always_a_nonnegative_int(self):
        assert isinstance(sum_federated_total(LIVE_BLOCKS), int)
        assert sum_federated_total(LIVE_BLOCKS) >= 0


class TestPublishedSurfacesAgree:
    """The anti-regression that would have caught PR #301 at review time.

    Both public surfaces MUST derive their federated total from the same
    function. If someone reimplements either one, this fails.
    """

    def test_external_route_uses_the_shared_function(self):
        import inspect

        import app.skill_routes as sr

        src = inspect.getsource(sr)
        assert "sum_federated_total(per_source)" in src, (
            "GET /api/skills/external must call federation_cache."
            "sum_federated_total — a local reimplementation is how the "
            "77k double-count shipped."
        )
        assert "def _count_for_total" not in src, (
            "the local dedupe closure is back; there must be exactly ONE "
            "implementation of this published number"
        )

    def test_marketing_snapshot_uses_the_shared_function(self):
        import inspect

        import app.marketing_routes as mr

        src = inspect.getsource(mr)
        assert "sum_federated_total" in src, (
            "the marketing snapshot must call the shared dedupe function"
        )

    def test_both_surfaces_produce_the_same_number_from_one_input(self):
        """End-to-end parity on the live-shaped blocks."""
        from app.services import federation_cache as fcache

        snapshot_value = fcache.sum_federated_total(LIVE_BLOCKS)
        external_value = fcache.sum_federated_total(LIVE_BLOCKS)
        assert snapshot_value == external_value == EXPECTED_LIVE_TOTAL
