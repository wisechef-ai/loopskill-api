"""bundles_0811 P3 follow-up — typing the source you were SHOWN must resolve.

THE GAP
-------
P3 (api#225) wired `hermes-hub:<slug>` and the worker flagged honestly that only
that one prefix was accepted. Probed live afterwards:

    GET /api/skills/install?slug=hermes-hub:official-security-1password   -> 200
    GET /api/skills/install?slug=skills-sh:coreyhaines31--...--seo-audit  -> 404
        {"detail": "Skill 'skills-sh:...' not found"}

WHY THAT MATTERED MORE THAN IT LOOKED
-------------------------------------
`hermes-hub` is the HUB NAMESPACE, not an upstream source. Verified on prod:

    select distinct upstream_source from federation_hub_skills
      -> browse-sh, claude-marketplace, clawhub, github, lobehub, official,
         skills-sh          <- `hermes-hub` is NOT among them

    select count(*), count(distinct slug) from federation_hub_skills
      -> 90605, 90605       <- slugs are GLOBALLY UNIQUE

So `hermes-hub:<skills-sh row>` already resolved 200 — confirmed live against
`hermes-hub:skills-sh-getpaseo-paseo-paseo-handoff` (repo `getpaseo/paseo`) and
`hermes-hub:nvidia-skills-...-cudf` (repo `NVIDIA/skills`). The row was always
reachable; only the name a user would naturally type was rejected.

That is the worst shape for this failure: a search card shows
`source: "skills-sh"`, the user types `skills-sh:<slug>`, and gets a bare
"Skill not found" — which reads as *we don't have it* when we demonstrably do.
Silent-wrong-answer, not loud-failure.

Because slugs are globally unique the prefix is a HINT, never a disambiguator,
so accepting any known upstream source as an alias is unambiguous by
construction. `ext:` stays excluded — it names a materialized LOCAL pointer row.
"""

from __future__ import annotations

from app.install_routes import _FEDERATED_SLUG_PREFIXES


class TestAcceptedPrefixes:
    def test_hub_namespace_is_accepted(self):
        assert "hermes-hub" in _FEDERATED_SLUG_PREFIXES

    def test_every_real_upstream_source_is_accepted(self):
        """The values a user actually sees on a search card must all work.

        Sourced from prod: select distinct upstream_source from
        federation_hub_skills (2026-08-11).
        """
        for src in (
            "browse-sh",
            "claude-marketplace",
            "clawhub",
            "github",
            "lobehub",
            "official",
            "skills-sh",
        ):
            assert src in _FEDERATED_SLUG_PREFIXES, (
                f"{src!r} is a live upstream_source — a user who reads it off a "
                f"result card and types {src}:<slug> must not get 'not found'"
            )

    def test_ext_is_NOT_accepted(self):
        """`ext:` names a materialized LOCAL pointer row, not a hub row.

        Routing it here would send a local Skill lookup into the federated
        resolver and 404 a row we hold.
        """
        assert "ext" not in _FEDERATED_SLUG_PREFIXES

    def test_prefix_set_is_immutable(self):
        """Frozen so no caller can mutate routing at runtime."""
        assert isinstance(_FEDERATED_SLUG_PREFIXES, frozenset)


class TestRouteDispatch:
    """The route must send every accepted prefix to the federated resolver."""

    def _dispatch_prefix(self, slug: str) -> str | None:
        """Mirror the route's dispatch predicate exactly."""
        if ":" in slug and not slug.startswith("ext:"):
            source, _sep, _rest = slug.partition(":")
            if source in _FEDERATED_SLUG_PREFIXES:
                return source
        return None

    def test_skills_sh_ref_dispatches_federated(self):
        assert (
            self._dispatch_prefix("skills-sh:coreyhaines31--marketingskills--seo-audit")
            == "skills-sh"
        )

    def test_hermes_hub_ref_still_dispatches_federated(self):
        assert self._dispatch_prefix("hermes-hub:official-security-1password") == "hermes-hub"

    def test_ext_ref_falls_through_to_local_lookup(self):
        assert self._dispatch_prefix("ext:skills-sh:coreyhaines31--x--y") is None

    def test_plain_local_slug_falls_through(self):
        assert self._dispatch_prefix("copywriting") is None

    def test_unknown_prefix_falls_through_to_local_lookup(self):
        """An unknown prefix must NOT be silently treated as federated.

        It falls through to the local lookup, whose 404 names the real problem.
        """
        assert self._dispatch_prefix("not-a-source:whatever") is None
