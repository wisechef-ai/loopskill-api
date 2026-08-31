"""The awesome-agent-skills tap: registration + first-class metasearch ranking.

Harness-agnostic agent-infrastructure skills (self-hosted inference, GPU serving,
deploy) federated from wisechef-ai/awesome-agent-skills. Registered with
in_metasearch=true so its skills rank first-class rather than only surfacing on
the legacy /external page.
"""

from app.services.github_taps import (
    GITHUB_FACET_SOURCES,
    METASEARCH_TAP_SOURCES,
    TAP_BY_SOURCE,
)

SOURCE = "github-awesome-agent-skills"


class TestAwesomeAgentSkillsTap:
    """Registration contract for the awesome-agent-skills tap."""

    def test_tap_is_registered(self):
        assert SOURCE in TAP_BY_SOURCE
        assert SOURCE in GITHUB_FACET_SOURCES

    def test_points_at_the_right_repo_and_path(self):
        tap = TAP_BY_SOURCE[SOURCE]
        assert tap.repo == "wisechef-ai/awesome-agent-skills"
        # Skills live under skills/<slug>/SKILL.md — the walker requires the dir.
        assert tap.path == "skills/"

    def test_mit_licensed_whole_repo(self):
        # Repo-root MIT => license resolves at the repo level, not per skill dir.
        assert TAP_BY_SOURCE[SOURCE].repo_license == "MIT"

    def test_trusted_source(self):
        assert TAP_BY_SOURCE[SOURCE].trust == "trusted-source"

    def test_ranks_first_class_in_metasearch(self):
        # in_metasearch=true is the "no external ghetto" wiring: without it the
        # skills only appear on the legacy /external surface.
        assert TAP_BY_SOURCE[SOURCE].in_metasearch is True
        assert SOURCE in METASEARCH_TAP_SOURCES

    def test_included_in_default_metasearch_fanout(self):
        from app.services.metasearch_fanout import DEFAULT_FANOUT_SOURCES

        assert SOURCE in DEFAULT_FANOUT_SOURCES
        # Fan-out must not double-count a source.
        assert len(DEFAULT_FANOUT_SOURCES) == len(set(DEFAULT_FANOUT_SOURCES))

    def test_source_id_is_unique(self):
        ids = [t.source_id for t in TAP_BY_SOURCE.values()]
        assert ids.count(SOURCE) == 1
